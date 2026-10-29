"""
Групповое обновление плат по Ethernet через mcumgr/SMP (UDP, порт 1337).

Для каждой платы (параллельно, не больше --parallel одновременно):
  inventory  → версия и состояние активного образа
  upload     → подписанный образ в свободный слот
  test       → пометить его на пробную загрузку (trailer slot1)
  reset      → MCUboot делает swap и запускает новый образ как "testing"
  health     → ждём ответа os echo по SMP, читаем состояние слотов
  verify     → активная версия == версии из заголовка образа
  confirm    → подтверждаем; иначе reset → MCUboot сам откатывает

Почему проверка идёт через SMP os echo, а не через UDP echo 4242: прошивка
подтверждает себя сама по первому ответу на UDP echo (CONFIG_SBER_OTA_AUTO_CONFIRM),
а решение "confirm или откат" здесь должен принимать оркестратор, уже сверив версию.

Запуск (venv активирован, из C:\\dev\\zephyrproject):
  python app/sber-ota/tools/fleet_update.py inventory --boards 192.168.7.1
  python app/sber-ota/tools/fleet_update.py update --boards 192.168.7.1 \\
         --image app/sber-ota/tools/sber-ota-0.1.4.signed.bin
Список плат — через --boards a,b,c или --boards-file (по одному IP в строке).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.mcuboot import ImageHeader
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import EchoWrite, ResetWrite
from smpclient.transport.udp import SMPUDPTransport

SMP_TIMEOUT = 5.0      # обычный запрос
UPLOAD_FIRST = 20.0    # первый кусок: img_mgmt стирает сектор 128 КБ (~1,9 с) — с запасом
UPLOAD_NEXT = 8.0      # следующие куски: стирание очередного сектора попадает внутрь

# smpclient пишет трейсбеки каждого таймаута через logging — для оркестратора
# таймаут это штатная ситуация (плата перезагружается), гасим.
logging.getLogger("smpclient").setLevel(logging.CRITICAL)
logging.getLogger("smp").setLevel(logging.CRITICAL)


def brief(e: BaseException) -> str:
    """Короткое описание исключения для сводки."""
    if isinstance(e, TimeoutError):
        return "no SMP answer (timeout)"
    return f"{type(e).__name__}: {str(e)[:80]}"


# ---------------------------------------------------------------- результат
@dataclass
class BoardResult:
    ip: str
    status: str = "PENDING"        # SKIPPED / UPDATED / ROLLED_BACK / UNREACHABLE / FAILED
    before: str = "?"
    after: str = "?"
    detail: str = ""
    upload_s: float = 0.0
    log: list[str] = field(default_factory=list)


def ver3(s: str) -> tuple[int, int, int]:
    """'0.1.4', '0.1.4+0', '0.1.4.0' → (0, 1, 4): build-номер не сравниваем."""
    parts = s.replace("+", ".").split(".")
    return tuple(int(p) for p in parts[:3])  # type: ignore[return-value]


def image_version(path: Path) -> str:
    hdr = ImageHeader.load_file(str(path))
    return f"{hdr.ver.major}.{hdr.ver.minor}.{hdr.ver.revision}"


# ---------------------------------------------------------------- одна плата
class Board:
    def __init__(self, ip: str, args: argparse.Namespace, image: bytes | None, target: str | None):
        self.ip = ip
        self.args = args
        self.image = image
        self.target = target
        self.res = BoardResult(ip)

    def log(self, msg: str) -> None:
        line = f"[{self.ip}] {msg}"
        self.res.log.append(line)
        print(line, flush=True)

    def client(self, timeout: float = SMP_TIMEOUT) -> SMPClient:
        return SMPClient(SMPUDPTransport(), self.ip, timeout_s=timeout)

    async def states(self, c: SMPClient) -> list:
        r = await c.request(ImageStatesRead())
        if error(r):
            raise RuntimeError(f"ImageStatesRead: {r}")
        return r.images

    @staticmethod
    def active(images: list):
        return next((i for i in images if i.active), None)

    # -- inventory ---------------------------------------------------------
    async def inventory(self) -> list | None:
        try:
            async with self.client() as c:
                imgs = await self.states(c)
        except Exception as e:  # noqa: BLE001
            self.res.status = "UNREACHABLE"
            self.res.detail = brief(e)
            self.log(f"unreachable: {self.res.detail}")
            return None
        act = self.active(imgs)
        self.res.before = act.version if act else "?"
        for i in imgs:
            flags = ",".join(f for f in ("active", "confirmed", "pending") if getattr(i, f))
            self.log(f"slot{i.slot}: v{i.version:<8} {flags or '-':<26} {i.hash.hex()[:16] if i.hash else ''}")
        return imgs

    # -- update ------------------------------------------------------------
    async def update(self) -> BoardResult:
        assert self.image is not None and self.target is not None
        imgs = await self.inventory()
        if imgs is None:
            return self.res
        act = self.active(imgs)
        if act is None:
            return self.fail("no active image reported")
        if ver3(act.version) == ver3(self.target):
            self.res.status, self.res.after = "SKIPPED", act.version
            self.res.detail = "already on target version"
            self.log("already on target version, skip")
            return self.res
        if not act.confirmed:
            return self.fail("active image is not confirmed (testing) — resolve before updating")
        if self.args.dry_run:
            self.res.status, self.res.detail = "DRY_RUN", f"would update {act.version} -> {self.target}"
            self.log(self.res.detail)
            return self.res

        # upload + test + reset — в одном соединении
        try:
            async with self.client() as c:
                t0 = time.perf_counter()
                total, last_pct = len(self.image), -10
                async for off in c.upload(self.image, first_timeout_s=UPLOAD_FIRST,
                                          subsequent_timeout_s=UPLOAD_NEXT):
                    pct = off * 100 // total
                    if pct >= last_pct + 25 or off == total:
                        self.log(f"upload {pct:3d}%  {off}/{total} B")
                        last_pct = pct
                self.res.upload_s = time.perf_counter() - t0
                self.log(f"upload done in {self.res.upload_s:.1f} s "
                         f"({total / 1024 / self.res.upload_s:.1f} KB/s)")

                imgs = await self.states(c)
                cand = next((i for i in imgs if not i.active and ver3(i.version) == ver3(self.target)), None)
                if cand is None or cand.hash is None:
                    return self.fail("uploaded image not visible in secondary slot")
                r = await c.request(ImageStatesWrite(hash=bytes(cand.hash), confirm=False))
                if error(r):
                    return self.fail(f"mark-for-test failed: {r}")
                pend = next((i for i in r.images if i.slot == cand.slot), None)
                if not (pend and pend.pending):
                    return self.fail("secondary slot not marked pending")
                self.log(f"slot{cand.slot} v{cand.version} marked for test, resetting")
                await c.request(ResetWrite())
        except Exception as e:  # noqa: BLE001
            return self.fail(brief(e))

        # после сброса: ждём SMP, проверяем версию
        imgs = await self.wait_alive(self.args.boot_timeout)
        if imgs is None:
            self.res.status = "UNREACHABLE"
            self.res.detail = ("no SMP answer after reset; image left unconfirmed — "
                               "MCUboot reverts it on next reset/power cycle")
            self.log(self.res.detail)
            await self.hw_reset_hook()
            return self.res

        act = self.active(imgs)
        if act and ver3(act.version) == ver3(self.target):
            try:
                async with self.client() as c:
                    r = await c.request(ImageStatesWrite(confirm=True))
                    if error(r):
                        return self.fail(f"confirm failed: {r}")
                    act2 = self.active(r.images)
                    if not (act2 and act2.confirmed):
                        return self.fail("confirm not reflected in state")
            except Exception as e:  # noqa: BLE001
                return self.fail(f"confirm: {brief(e)}")
            self.res.status, self.res.after = "UPDATED", act.version
            self.log(f"v{self.res.before} -> v{act.version} confirmed")
            return self.res

        got = act.version if act else "?"
        self.log(f"booted v{got}, expected v{self.target}: rolling back")
        return await self.rollback()

    async def rollback(self) -> BoardResult:
        """Образ не подтверждён → сброс → MCUboot сам возвращает предыдущий."""
        try:
            async with self.client() as c:
                await c.request(ResetWrite())
        except Exception as e:  # noqa: BLE001
            return self.fail(f"rollback reset: {brief(e)}")
        imgs = await self.wait_alive(self.args.boot_timeout)
        act = self.active(imgs) if imgs else None
        self.res.after = act.version if act else "?"
        if act and ver3(act.version) == ver3(self.res.before):
            self.res.status, self.res.detail = "ROLLED_BACK", "new image failed verification"
            self.log(f"rolled back to v{act.version}")
        else:
            self.fail(f"rollback result unclear, active v{self.res.after}")
        return self.res

    async def wait_alive(self, timeout: float) -> list | None:
        """Ждём, пока плата ответит на SMP os echo, затем читаем состояние слотов."""
        deadline = time.monotonic() + timeout
        await asyncio.sleep(2.0)  # сброс + линк PHY ~1,6 с
        while time.monotonic() < deadline:
            try:
                async with self.client(timeout=2.0) as c:
                    r = await c.request(EchoWrite(d="fleet"))
                    if success(r) and r.r == "fleet":
                        imgs = await self.states(c)
                        act = self.active(imgs)
                        self.log(f"alive after reset, active v{act.version if act else '?'} "
                                 f"confirmed={act.confirmed if act else '?'}")
                        return imgs
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)
        return None

    async def hw_reset_hook(self) -> None:
        """Опционально: внешняя команда сброса (ST-Link, реле питания), затем проверка отката."""
        if not self.args.on_unreachable_cmd:
            return
        self.log(f"running hook: {self.args.on_unreachable_cmd}")
        proc = await asyncio.create_subprocess_shell(
            self.args.on_unreachable_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        imgs = await self.wait_alive(self.args.boot_timeout)
        act = self.active(imgs) if imgs else None
        if act and ver3(act.version) == ver3(self.res.before):
            self.res.status, self.res.after = "ROLLED_BACK", act.version
            self.res.detail = "new image never answered; MCUboot reverted after external reset"
            self.log(f"rolled back to v{act.version} by MCUboot")
        elif act:
            self.res.after = act.version
            self.fail(f"after hook active v{act.version}, expected v{self.res.before}")

    def fail(self, why: str) -> BoardResult:
        self.res.status, self.res.detail = "FAILED", why
        self.log(f"FAILED: {why}")
        return self.res


# ---------------------------------------------------------------- main
def load_boards(args: argparse.Namespace) -> list[str]:
    boards: list[str] = []
    if args.boards:
        boards += [b.strip() for b in args.boards.split(",") if b.strip()]
    if args.boards_file:
        boards += [ln.strip() for ln in Path(args.boards_file).read_text().splitlines()
                   if ln.strip() and not ln.startswith("#")]
    if not boards:
        sys.exit("no boards: use --boards or --boards-file")
    return boards


async def run(args: argparse.Namespace) -> int:
    boards = load_boards(args)
    image = target = None
    if args.cmd == "update":
        image = Path(args.image).read_bytes()
        target = image_version(Path(args.image))
        print(f"image {args.image}: v{target}, {len(image)} B, "
              f"sha256 {hashlib.sha256(image).hexdigest()[:16]}…")
    sem = asyncio.Semaphore(args.parallel)

    async def one(ip: str) -> BoardResult:
        b = Board(ip, args, image, target)
        async with sem:
            if args.cmd == "inventory":
                imgs = await b.inventory()
                if imgs is not None:
                    b.res.status = "OK"
                return b.res
            return await b.update()

    results = await asyncio.gather(*(one(ip) for ip in boards))

    print("\n=== summary ===")
    print(f"{'board':<16} {'status':<12} {'before':<8} {'after':<8} {'upload':>7}  detail")
    rc = 0
    for r in results:
        up = f"{r.upload_s:.1f}s" if r.upload_s else ""
        print(f"{r.ip:<16} {r.status:<12} {r.before:<8} {r.after:<8} {up:>7}  {r.detail}")
        if r.status in ("FAILED", "UNREACHABLE"):
            rc = 1
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["inventory", "update"])
    ap.add_argument("--boards", help="IP-адреса через запятую")
    ap.add_argument("--boards-file", help="файл со списком IP")
    ap.add_argument("--image", help="подписанный .bin (для update)")
    ap.add_argument("--parallel", type=int, default=4, help="сколько плат обновлять одновременно")
    ap.add_argument("--boot-timeout", type=float, default=90.0,
                    help="сколько ждать ответа после сброса (swap ~25 с на 218 КБ)")
    ap.add_argument("--dry-run", action="store_true", help="только инвентарь и решение, без upload")
    ap.add_argument("--on-unreachable-cmd",
                    help="команда внешнего сброса платы, если новый образ не ответил "
                         "(например STM32_Programmer_CLI -c port=swd -rst)")
    args = ap.parse_args()
    if args.cmd == "update" and not args.image:
        ap.error("update requires --image")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
