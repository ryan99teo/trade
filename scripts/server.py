"""Local server lifecycle manager (macOS/Linux, standard library only)."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".run"
PID_FILE = RUNTIME / "server.json"
LOG_FILE = RUNTIME / "server.log"
PYTHON = ROOT / ".venv" / "bin" / "python"
URL = "http://127.0.0.1:8000"
SCRIPT = str(Path(__file__).resolve())


def identity(record):
    """Check the private launch token before signaling a possibly reused PID."""
    try:
        pid = int(record["pid"])
        token = record["token"]
        if pid <= 1 or not isinstance(token, str) or not token:
            return False
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=", "-o", "command="],
                                capture_output=True, text=True)
        if result.returncode or not result.stdout.strip():
            return False
        state, command = result.stdout.strip().split(None, 1)
        return not state.startswith("Z") and (SCRIPT + " _serve " + token) in command
    except (KeyError, ValueError, TypeError):
        return False


def read_record():
    try:
        record = json.loads(PID_FILE.read_text())
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def port_in_use():
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", 8000)) == 0


def healthy():
    try:
        # A system proxy must not intercept local readiness checks.
        with build_opener(ProxyHandler({})).open(URL + "/api/status", timeout=1) as response:
            data = json.load(response)
            return isinstance(data, dict) and data.get("mode") == "paper" and data.get("market") == "KRW-BTC"
    except (OSError, ValueError):
        return False


def ensure_environment():
    if not PYTHON.exists():
        print("첫 실행: Python 가상환경을 준비합니다.", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    check = subprocess.run([str(PYTHON), "-c", "import fastapi, uvicorn"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        print("첫 실행: 필요한 패키지를 설치합니다.", flush=True)
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)


def start(open_browser=False):
    record = read_record()
    if identity(record):
        print("서버가 이미 실행 중입니다. " + URL)
        if open_browser:
            webbrowser.open(URL)
        return
    if port_in_use():
        raise RuntimeError("8000 포트를 다른 프로세스가 사용 중입니다. 해당 프로세스를 먼저 종료하세요.")
    ensure_environment()
    token = uuid.uuid4().hex
    with LOG_FILE.open("a") as log:
        child = subprocess.Popen([str(PYTHON), SCRIPT, "_serve", token], cwd=str(ROOT),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    record = {"pid": child.pid, "token": token}
    PID_FILE.write_text(json.dumps(record))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if child.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            raise RuntimeError("서버 시작에 실패했습니다. ./server logs 로 로그를 확인하세요.")
        if healthy():
            print("서버를 시작했습니다. " + URL)
            print("자동매매는 정지 상태입니다. 화면에서 거래 시작을 눌러주세요.")
            if open_browser:
                webbrowser.open(URL)
            return
        time.sleep(.2)
    # Keep ownership metadata so a slow-starting server can still be stopped.
    raise RuntimeError("서버 응답 대기 시간이 지났습니다. ./server status 또는 ./server logs 로 확인하세요.")


def stop():
    record = read_record()
    if not identity(record):
        PID_FILE.unlink(missing_ok=True)
        print("이 실행기로 시작한 서버가 없습니다.")
        if port_in_use():
            print("8000 포트에는 별도로 실행한 프로세스가 있습니다. 해당 터미널에서 종료하세요.")
        return
    os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 25
    while identity(record) and time.monotonic() < deadline:
        time.sleep(.2)
    if identity(record):
        raise RuntimeError("서버가 아직 종료 중입니다. 잠시 후 ./server stop 을 다시 실행하세요.")
    PID_FILE.unlink(missing_ok=True)
    print("서버와 시세 수집을 중지했습니다. 저장된 잔고와 기록은 유지됩니다.")


def main():
    # Worker command retains a unique token in its process arguments.
    if len(sys.argv) == 3 and sys.argv[1] == "_serve":
        os.chdir(ROOT)
        sys.path.insert(0, str(ROOT))
        import uvicorn
        uvicorn.run("trade.app:create_app", factory=True, host="127.0.0.1", port=8000, workers=1)
        return
    parser = argparse.ArgumentParser(description="BTC 로컬 서버 시작·중지")
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "logs"))
    parser.add_argument("--open", action="store_true", help="시작 후 관리 화면 열기")
    args = parser.parse_args()
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    # Serializes concurrent double-clicks and stop/restart requests.
    with (RUNTIME / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "start":
            start(args.open)
        elif args.action == "stop":
            stop()
        elif args.action == "restart":
            stop()
            start(args.open)
        elif args.action == "status":
            if identity(read_record()):
                print("실행 중 · {} · {}".format(URL, "응답 정상" if healthy() else "응답 대기/오류"))
            elif port_in_use():
                print("관리 서버 정지 · 8000 포트에 별도로 실행한 프로세스가 있습니다.")
            else:
                print("서버 정지 상태입니다.")
        elif args.action == "logs":
            print("로그: " + str(LOG_FILE))
            if LOG_FILE.exists():
                print("".join(LOG_FILE.read_text(errors="replace").splitlines(keepends=True)[-80:]), end="")
            else:
                print("아직 로그가 없습니다.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print("오류: " + str(exc), file=sys.stderr)
        sys.exit(1)
