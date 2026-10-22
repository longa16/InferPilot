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
    if not re.search(r'^GEMINI_API_KEY=', text, re.MULTILINE):
        text += '\n# Google Gemini API key for the InferPilot agent.\nGEMINI_API_KEY=\n'
    if not re.search(r'^AGENT_MODEL=', text, re.MULTILINE):
        text += 'AGENT_MODEL=gemini-flash-latest\n'
    path.write_text(text)
    try: path.chmod(0o600)
    except OSError: pass
    print('Local configuration prepared; existing values preserved. Set GROQ_API_KEY for NovaMaison, GEMINI_API_KEY for the agent, and their accessible model names. Existing AGENT_MODEL values are preserved; change an old Groq model to a Gemini model manually. No secrets printed.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--path',type=Path,default=ROOT/'.env')
    configure(parser.parse_args().path)
