"""
RWW-тест: непрерывно пингуем UDP echo на плате и параллельно через консоль
ST-Link запускаем стирание/запись/проверку slot1 (банк 2).
На выходе — RTT и потери по фазам: до, erase, write, verify, после.

Запуск (venv активирован):
    python app/sber-ota/tools/rww_test.py [--port COM10] [--board 192.168.7.1] [--interval 0.02]
"""
import argparse
import re
import socket
import statistics
import threading
import time

import serial

MARK = re.compile(r"#RWW (\w+) done")


class Pinger(threading.Thread):
    """Шлёт датаграмму seq+ts каждые interval секунд, отдельно принимает ответы."""

    def __init__(self, board, port, interval):
        super().__init__(daemon=True)
        self.addr = (board, port)
        self.interval = interval
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)
        self.sent = {}      # seq -> t_send
        self.rtt = {}       # seq -> (t_send, rtt_ms)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.rx = threading.Thread(target=self._rx, daemon=True)

    def run(self):
        self.rx.start()
        seq = 0
        nxt = time.perf_counter()
        while not self.stop.is_set():
            now = time.perf_counter()
            with self.lock:
                self.sent[seq] = now
            self.sock.sendto(f"{seq}".encode(), self.addr)
            seq += 1
            nxt += self.interval
            dt = nxt - time.perf_counter()
            if dt > 0:
                time.sleep(dt)

    def _rx(self):
        while not self.stop.is_set():
            try:
                data, _ = self.sock.recvfrom(64)
            except socket.timeout:
                continue
            now = time.perf_counter()
            seq = int(data)
            with self.lock:
                t = self.sent.pop(seq, None)
                if t is not None:
                    self.rtt[seq] = (t, (now - t) * 1000)

    def stats(self, t_from, t_to):
        with self.lock:
            ok = [r for (t, r) in self.rtt.values() if t_from <= t < t_to]
            lost = [s for s, t in self.sent.items() if t_from <= t < t_to - 1.0]
        n = len(ok) + len(lost)
        if not ok:
            return f"sent {n}, lost {len(lost)}, no replies"
        ok.sort()
        p99 = ok[int(len(ok) * 0.99) - 1] if len(ok) > 1 else ok[0]
        return (f"sent {n:4d} lost {len(lost):3d} | RTT min {ok[0]:.2f} "
                f"avg {statistics.mean(ok):.2f} p99 {p99:.2f} max {ok[-1]:.2f} ms")


def run_cmd(ser, cmd, log, timeout=120):
    """Отправить shell-команду, читать строки до маркера '#RWW <cmd> done'."""
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    t0 = time.perf_counter()
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        line = ser.readline().decode("utf-8", "replace")
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
        if not line or line.endswith(cmd):
            continue
        log.append((time.perf_counter(), line))
        if MARK.search(line):
            break
    return t0, time.perf_counter()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM10")
    ap.add_argument("--board", default="192.168.7.1")
    ap.add_argument("--udp", type=int, default=4242)
    ap.add_argument("--interval", type=float, default=0.02)
    ap.add_argument("--settle", type=float, default=3.0)
    args = ap.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.5)
    ser.write(b"\r")
    time.sleep(0.3)
    ser.reset_input_buffer()

    pinger = Pinger(args.board, args.udp, args.interval)
    pinger.start()
    time.sleep(1.0)  # ARP и прогрев

    log = []
    phases = []
    t = time.perf_counter()
    time.sleep(args.settle)
    phases.append(("baseline", t, time.perf_counter()))

    for cmd in ("rww erase", "rww write", "rww verify"):
        print(f"--> {cmd}")
        a, b = run_cmd(ser, cmd, log)
        phases.append((cmd.split()[1], a, b))

    t = time.perf_counter()
    time.sleep(args.settle)
    phases.append(("after", t, time.perf_counter()))
    time.sleep(1.0)  # дождаться поздних ответов
    pinger.stop.set()
    ser.close()

    print("\n=== Board log (host time, s from start) ===")
    t_start = phases[0][1]
    for ts, line in log:
        print(f"{ts - t_start:7.3f}  {line}")

    print("\n=== RTT per phase ===")
    for name, a, b in phases:
        print(f"{name:9s} {b - a:6.2f} s | {pinger.stats(a, b)}")

    # RTT по каждому стиранию сектора: интервалы между строками '#RWW erase sector'
    print("\n=== RTT during each sector erase ===")
    prev = None
    for ts, line in log:
        m = re.search(r"erase sector (\d+) .* (\d+) ms", line)
        if m:
            start = ts - int(m.group(2)) / 1000
            print(f"sector {m.group(1):>2}: {pinger.stats(start, ts)}")
            prev = ts


if __name__ == "__main__":
    main()
