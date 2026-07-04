python.exe .\knigavuhe.org_grabber_v3
usage: audiobook.py [-h] [-t THREADS] [url]

Audiobook downloader

positional arguments:
  url                   URL book

options:
  -h, --help            show this help message and exit
  -t, --threads THREADS
                        Thread number (default 5)

Example:

python audiobook.py https://site/book/123

or

python audiobook.py https://site/book/123 -t 10
