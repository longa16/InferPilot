"""Create local configuration without exposing credentials or replacing existing values."""
from pathlib import Path
import secrets
import re
import argparse

ROOT = Path(__file__).resolve().parents[1]


def configure(path):
    text = path.read_text() if path.exists() else (ROOT/'.env.example').read_text()
    for key in ('POSTGRES_PASSWORD','ADMIN_AUTH_SECRET','HMAC_SECRET'):
        pattern = re.compile(r'^'+key+r'=(.*)$',re.MULTILINE)
        found = pattern.search(text)
        if found and found.group(1).strip():
            continue
        value = secrets.token_hex(32)
        if found: text = text[:found.start()]+key+'='+value+text[found.end():]
        else: text += '\n'+key+'='+value+'\n'
    path.write_text(text)
    try: path.chmod(0o600)
    except OSError: pass
    print('Local configuration prepared; existing values preserved. Set GROQ_API_KEY and an accessible model in this file. No secrets printed.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--path',type=Path,default=ROOT/'.env')
    configure(parser.parse_args().path)
