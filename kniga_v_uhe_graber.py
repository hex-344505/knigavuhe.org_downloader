#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import concurrent.futures
import logging
import os
import re
import sys
import threading
import time
import html
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

TIMEOUT = 30
DOWNLOAD_THREADS = 8
RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "uk,en-US;q=0.8,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

session = requests.Session()
session.headers.update(HEADERS)

print_lock = threading.Lock()


def print_safe(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def create_parser():
    parser = argparse.ArgumentParser(
        prog="audiobook_downloader.py",
        description="Завантаження аудіокниг зі сторінки.",
        epilog="""
Приклад:

python audiobook_downloader.py https://site/book/123

Скрипт автоматично:

 • знаходить var player;
 • отримує назву книги;
 • створює каталог;
 • знаходить всі MP3;
 • створює output.txt;
 • завантажує всі файли.
""",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="URL сторінки книги"
    )

    return parser


def sanitize_filename(name: str) -> str:
    """
    Робить назву безпечною для Windows/Linux,
    залишаючи кирилицю.
    """

    bad = '<>:"/\\|?*'

    for ch in bad:
        name = name.replace(ch, "_")

    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = "Book"

    return name


def download_html(url: str) -> str:

    print_safe("Завантаження сторінки...")

    response = session.get(url, timeout=TIMEOUT)

    response.raise_for_status()

    response.encoding = response.apparent_encoding

    return response.text


def save_url_list(urls, folder: Path):

    output = folder / "output.txt"

    with open(output, "w", encoding="utf-8") as f:
        for u in urls:
            f.write(u + "\n")


def setup_logging(folder: Path):

    logging.basicConfig(
        filename=folder / "download.log",
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        encoding="utf-8",
    )


def download_file(index, total, url, folder: Path):

    filename = os.path.basename(urlparse(url).path)
    filename = unquote(filename)

    if not filename:
        filename = f"{index:03d}.mp3"

    filepath = folder / filename

    if filepath.exists():
        print_safe(f"[{index:03d}/{total}] ✓ вже існує")
        return "skipped"

    for attempt in range(RETRIES):

        try:

            response = session.get(
                url,
                stream=True,
                timeout=TIMEOUT,
            )

            response.raise_for_status()

            with open(filepath, "wb") as out:

                for chunk in response.iter_content(65536):
                    if chunk:
                        out.write(chunk)

            print_safe(f"[{index:03d}/{total}] ✓ {filename}")

            logging.info("OK %s", filename)

            return "ok"

        except Exception as e:

            if attempt == RETRIES - 1:

                print_safe(f"[{index:03d}/{total}] ✗ {filename}")

                logging.error("%s : %s", filename, e)

                return "error"
            
def extract_player_block(html: str) -> str:
    """
    Знаходить блок JavaScript, який починається з 'var player'
    і закінчується закриваючим </script>.
    """

    marker = "var player"

    pos = html.find(marker)

    if pos == -1:
        raise RuntimeError("Не вдалося знайти 'var player'.")

    script_end = html.find("</script>", pos)

    if script_end == -1:
        raise RuntimeError("Не знайдено кінець <script>.")

    block = html[pos:script_end]

    with open("player_dump.js", "w", encoding="utf-8") as f:
        f.write(block)

    return block

def decode_json_string(text: str) -> str:
    if not text:
        return text

    # HTML entities
    text = html.unescape(text)

    # JSON escapes
    text = (
        text
        .replace("\\/", "/")
        .replace("\\\"", "\"")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
    )

    # URL encoding (%D0...)
    try:
        text = unquote(text)
    except Exception:
        pass

    # _u041A -> \u041A
    text = re.sub(r"_u([0-9A-Fa-f]{4})", r"\\u\1", text)

    # Якщо залишились \uXXXX — декодуємо їх
    try:
        text = text.encode("utf-8").decode("unicode_escape")
    except Exception:
        pass

    # Іноді рядок подвійно закодований
    try:
        text = text.encode("latin1").decode("utf-8")
    except Exception:
        pass

    return text.strip()

def extract_book_info(player_code: str):
    """
    Отримує authors та title з блоку player_data.

    Повертає:
        (authors, title)
    """

    marker = '"player_data":{'

    pos = player_code.find(marker)

    if pos == -1:
        print("Не знайдено player_data.")

        return (
            "Unknown author",
            extract_first_title(player_code)
        )

    # знаходимо кінець player_data
    start = pos + len(marker)

    depth = 1
    in_string = False
    escaped = False

    end = None

    for i in range(start, len(player_code)):

        c = player_code[i]

        if in_string:

            if escaped:
                escaped = False

            elif c == "\\":
                escaped = True

            elif c == '"':
                in_string = False

            continue

        if c == '"':
            in_string = True

        elif c == "{":
            depth += 1

        elif c == "}":
            depth -= 1

            if depth == 0:
                end = i
                break

    if end is None:
        raise RuntimeError("Не вдалося розібрати player_data.")

    player_data = player_code[start:end]

    def get_field(name, default=""):

        marker = f'"{name}":"'

        p = player_data.find(marker)

        if p == -1:
            return default

        p += len(marker)

        q = player_data.find('"', p)

        if q == -1:
            return default

        value = player_data[p:q]

        value = decode_json_string(value)

        value = sanitize_filename(value)

        return value

    authors = get_field("authors", "Unknown author")
    title = get_field("title", "Book")

    return authors, title

def extract_urls(player_code: str):
    """
    Знаходить всі URL MP3.
    """

    urls = []

    marker = '"url":"'

    start = 0

    while True:

        pos = player_code.find(marker, start)

        if pos == -1:
            break

        pos += len(marker)

        end = player_code.find('"', pos)

        if end == -1:
            break

        url = player_code[pos:end]

        url = decode_json_string(url)

        urls.append(url)

        start = end

    return urls


def prepare_book(html: str):
    """
    Повертає:
        title
        folder
        urls
    """

    player = extract_player_block(html)
 
    urls = extract_urls(player)

    if not urls:
        raise RuntimeError("MP3 не знайдено.")

    authors, title = extract_book_info(player)

    folder_name = f"{authors} - {title}"

    folder_name = sanitize_filename(folder_name)

    folder = Path(folder_name)
    
    folder.mkdir(exist_ok=True)

    setup_logging(folder)

    save_url_list(urls, folder)

    print()
    print("=" * 70)
    print("Назва книги :", title)
    print("Каталог     :", folder)
    print("MP3 знайдено:", len(urls))
    print("=" * 70)
    print()

    print("Список MP3:\n")

    for i, url in enumerate(urls, 1):
        print(f"{i:03d}. {url}")

    print()

    return title, folder, urls
def main():

    parser = create_parser()

    # запуск без параметрів
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    start_time = time.time()

    ok = 0
    skipped = 0
    errors = 0

    try:

        html = download_html(args.url)

        title, folder, urls = prepare_book(html)

        total = len(urls)

        print("Починається завантаження...\n")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=DOWNLOAD_THREADS
        ) as executor:

            futures = []

            for index, url in enumerate(urls, start=1):

                futures.append(
                    executor.submit(
                        download_file,
                        index,
                        total,
                        url,
                        folder
                    )
                )

            for future in concurrent.futures.as_completed(futures):

                result = future.result()

                if result == "ok":
                    ok += 1

                elif result == "skipped":
                    skipped += 1

                else:
                    errors += 1

    except KeyboardInterrupt:

        print("\n\nПерервано користувачем.")

        sys.exit(1)

    except Exception as e:

        print(f"\nПомилка:\n{e}")

        sys.exit(1)

    elapsed = time.time() - start_time

    print("\n" + "=" * 70)

    print("Готово.")

    print(f"Книга          : {title}")
    print(f"Каталог        : {folder}")
    print(f"Успішно        : {ok}")
    print(f"Пропущено      : {skipped}")
    print(f"Помилки        : {errors}")
    print(f"Всього MP3     : {len(urls)}")
    print(f"Час            : {elapsed:.1f} сек")

    print("=" * 70)


if __name__ == "__main__":
    main()