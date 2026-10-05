"""
Automasi presensi SPADA UPN (Moodle) + notifikasi Telegram.

Alur:
    1. Muat konfigurasi dari .env
    2. Buka Chromium (headless) via Playwright
    3. Login ke https://spada.upnyk.ac.id
    4. Navigasi ke halaman attendance tiap mata kuliah
    5. Deteksi sesi aktif -> submit "Hadir/Present"
    6. Kirim notifikasi Telegram (sukses / gagal) per mata kuliah + ringkasan

Contoh:
    python main.py
    python main.py --headed            # tampilkan browser untuk debug
    python main.py --url 12345 --url https://.../view.php?id=67890
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Locator,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------

BASE_URL = "https://spada.upnyk.ac.id"
LOGIN_URL = f"{BASE_URL}/login/index.php"
ATTENDANCE_URL_TEMPLATE = f"{BASE_URL}/mod/attendance/view.php?id={{cmid}}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEZONE = "Asia/Jakarta"  # WIB

_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def xpath_text_ci(tag: str, keyword: str) -> str:
    """Bangun XPath case-insensitive untuk mencari `keyword` di teks sebuah elemen."""
    return (
        f"//{tag}[contains(translate(., '{_UPPER}', '{_UPPER.lower()}'), '{keyword}')]"
    )

# Pola teks status absensi (Indonesia & Inggris).
PRESENT_PATTERN = re.compile(r"\b(present|hadir|terlambat|late)\b", re.IGNORECASE)
SUBMIT_PATTERN = re.compile(
    r"(submit\s*kehadiran|submit\s*attendance|isi\s*kehadiran|absen)", re.IGNORECASE
)
NOT_OPEN_PATTERN = re.compile(
    r"(not\s*yet\s*open|belum\s*(dibuka|mulai)|closed|sudah\s*tutup|selesai)", re.IGNORECASE
)
ERROR_PATTERN = re.compile(
    r"(invalid\s*login|login\s*(failed|incorrect)|username\s*or\s*password|"
    r"akun\s*tidak|gagal\s*login)",
    re.IGNORECASE,
)

# Kumpulan selector kandidat. Markup Moodle sering berubah, jadi selalu fallback.
SEL_USERNAME = [
    "#username",
    "input[name='username']",
    "input[type='text'][autocomplete='username']",
]
SEL_PASSWORD = ["#password", "input[name='password']"]
SEL_LOGIN_SUBMIT = ["button#loginbtn", "button[type='submit']", "input[type='submit']"]
SEL_LOGIN_ERROR = [
    "#loginerrormessage",
    ".alert-danger",
    "//div[contains(@class,'alert') and contains(@class,'danger')]",
]
SEL_SESSION_ROW = [
    "table#attendancetable tr",
    "table.generaltable tr",
    "//table[contains(@class,'generaltable')]//tr",
]
SEL_SUBMIT_LINK = [
    "a[href*='attendance_submit']",
    xpath_text_ci("a", "kehadiran"),
    xpath_text_ci("a", "submit"),
]
SEL_STATUS_CELL = ["td.attendancestatus", "td.attendance-status"]
SEL_PRESENCE_RADIO = [
    "input[type='radio'][name='presence']",
    "input[type='radio'][name='attendance']",
    "input[type='radio']",
]
SEL_SAVE_BUTTON = [
    "input[name='submitbutton']",
    "button[name='submitbutton']",
    "button[type='submit']",
    "input[type='submit']",
]
SEL_SUCCESS_BANNER = [
    ".alert-success",
    "//div[contains(@class,'alert') and contains(@class,'success')]",
]


class SpadaError(RuntimeError):
    """Error domain: login gagal, sesi tidak ditemukan, dan sejenisnya."""


# ---------------------------------------------------------------------------
# Model data
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Config:
    """Konfigurasi runtime yang dibaca dari environment variable."""

    username: str
    password: str
    telegram_token: str
    telegram_chat_id: str
    headless: bool = True
    attendance_targets: list[str] = field(default_factory=list)
    skip_courses: list[str] = field(default_factory=list)
    timeout_ms: int = 20_000
    user_agent: str = DEFAULT_USER_AGENT
    artifact_dir: Path = Path("artifacts")


@dataclass(slots=True)
class AttendanceResult:
    """Hasil proses attendance untuk satu mata kuliah."""

    course: str
    status: str  # success | already | not_open | failed
    reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now())

    @property
    def ok(self) -> bool:
        """True bila sesi terisi (baru atau sudah terisi sebelumnya)."""
        return self.status in {"success", "already"}


# ---------------------------------------------------------------------------
# Utilitas
# ---------------------------------------------------------------------------


def now_local() -> datetime:
    """Waktu sekarang dalam zona WIB."""
    return datetime.now().astimezone()


def log(message: str) -> None:
    """Cetak log bergaya timestamp ke stdout."""
    print(f"[{now_local():%H:%M:%S}] {message}", flush=True)


def clean_text(value: str | None) -> str:
    """Ratakan whitespace-newline dari hasil text_content()."""
    return re.sub(r"\s+", " ", value or "").strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _split_list(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def load_config(cli_urls: Sequence[str] = (), headed: bool | None = None) -> Config:
    """
    Muat konfigurasi dari .env, lalu timpa dengan argumen CLI bila tersedia.

    Args:
        cli_urls: Target attendance dari flag --url (cmid atau URL penuh).
        headed: True memaksa browser tampil, None mengikuti HEADLESS_MODE.

    Raises:
        SystemExit: bila kredensial wajib atau target attendance tidak ditemukan.
    """
    load_dotenv()

    required = {
        "SPADA_USERNAME": os.getenv("SPADA_USERNAME", "").strip(),
        "SPADA_PASSWORD": os.getenv("SPADA_PASSWORD", ""),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "Konfigurasi belum lengkap: " + ", ".join(missing) + "\n"
            "Salin .env.example menjadi .env lalu isi nilainya."
        )

    targets = list(cli_urls) or _split_list(os.getenv("SPADA_ATTENDANCE_URLS"))
    if not targets:
        raise SystemExit("Tidak ada target attendance. Set SPADA_ATTENDANCE_URLS atau pakai --url.")

    return Config(
        username=required["SPADA_USERNAME"],
        password=required["SPADA_PASSWORD"],
        telegram_token=required["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=required["TELEGRAM_CHAT_ID"],
        headless=_env_bool("HEADLESS_MODE", True) if headed is None else not headed,
        attendance_targets=targets,
        skip_courses=[item.lower() for item in _split_list(os.getenv("SKIP_COURSES"))],
        timeout_ms=int(os.getenv("STEP_TIMEOUT_MS", "20000")),
        user_agent=os.getenv("USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        artifact_dir=Path(os.getenv("ARTIFACT_DIR", "artifacts")),
    )


def resolve_target(target: str) -> str:
    """Terjemahkan cmid atau URL attendance menjadi URL absolut."""
    target = target.strip()
    if target.startswith("http://") or target.startswith("https://"):
        return target
    return ATTENDANCE_URL_TEMPLATE.format(cmid=target)


async def guess_course_name(page: Page) -> str:
    """Ambil nama mata kuliah dari judul halaman atau breadcrumb Moodle."""
    for selector in ("h1", "h2", ".topic-title", "title"):
        try:
            text = clean_text(await page.text_content(selector))
        except PlaywrightError:
            continue
        if text:
            return text[:120]
    return "Tidak diketahui"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")


class TelegramNotifier:
    """Kirim pesan ke Telegram Bot API lewat HTTP POST."""

    def __init__(self, token: str, chat_id: str, timeout: float = 15.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    async def send(self, text: str) -> bool:
        """
        Kirim pesan dan kembalikan True bila Telegram merespons ok.

        Kegagalan Telegram tidak pernah menggagalkan proses presensi; error
        hanya dicatat ke log.
        """
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        url = f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log(f"! Gagal kirim notifikasi Telegram: {exc}")
            return False

        if response.status_code != 200:
            log(f"! Telegram HTTP {response.status_code}: {response.text[:200]}")
            return False
        return True

    @staticmethod
    def format_success(course: str, when: datetime, note: str = "") -> str:
        """Susun pesan Markdown untuk sesi yang berhasil diisi."""
        detail = f"\n• *Catatan:* {note}" if note else ""
        return (
            "✅ *Presensi SPADA Berhasil!*\n"
            f"• *Mata Kuliah:* {course}\n"
            f"• *Tanggal/Waktu:* {when:%d %B %Y %H:%M} WIB\n"
            "• *Status:* Hadir / Present"
            f"{detail}"
        )

    @staticmethod
    def format_failure(course: str, when: datetime, reason: str) -> str:
        """Susun pesan Markdown untuk sesi gagal atau tidak ditemukan."""
        return (
            "⚠️ *Presensi SPADA Gagal / Tidak Ditemukan*\n"
            f"• *Mata Kuliah:* {course}\n"
            f"• *Waktu Cek:* {when:%d %B %Y %H:%M} WIB\n"
            f"• *Penyebab:* {reason}"
        )


# ---------------------------------------------------------------------------
# Otomasi SPADA
# ---------------------------------------------------------------------------


async def first_visible(
    page: Page | Locator, selectors: Iterable[str], timeout: float = 1.5
) -> Locator | None:
    """
    Kembalikan Locator pertama yang terlihat, atau None.

    Playwright tidak mendukung "coba banyak selector sekaligus", jadi tiap
    kandidat diuji singkat dan yang tidak ketemu/tertimpa dianggap gagal.
    """
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=timeout):
                return locator
        except PlaywrightError:
            continue
    return None


class SpadaBot:
    """Otomatisasi Moodle: login, deteksi sesi attendance, dan submit kehadiran."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._playwright: Any = None
        self._browser: Any = None
        self.context: Any = None
        self.page: Page | None = None
        config.artifact_dir.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Jalankan Playwright, launch Chromium, set viewport dan User-Agent."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self.context = await self._browser.new_context(
            user_agent=self.config.user_agent,
            viewport={"width": 1440, "height": 900},
            locale="id-ID",
            timezone_id=TIMEZONE,
        )
        self.context.set_default_timeout(self.config.timeout_ms)
        self.page = await self.context.new_page()
        log(f"Browser siap (headless={self.config.headless})")

    async def close(self) -> None:
        """Tutup context, browser, dan driver; aman dipanggil berkali-kali."""
        for closer in (self.context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    pass
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        log("Browser ditutup")

    # -- langkah 3: autentikasi -------------------------------------------

    async def login(self) -> None:
        """
        Login ke SPADA lalu pastikan sesi terautentikasi.

        Raises:
            SpadaError: bila form login tidak ditemukan atau kredensial ditolak.
        """
        assert self.page is not None
        page = self.page
        log("Membuka halaman login ...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await self._settle()

        if await self._is_logged_in():
            log("Sesi login masih aktif, melewati autentikasi")
            return

        username = await first_visible(page, SEL_USERNAME, timeout=5.0)
        password = await first_visible(page, SEL_PASSWORD, timeout=5.0)
        if not username or not password:
            raise SpadaError("Selector form login tidak ditemukan (mungkin sudah berubah)")

        await username.fill(self.config.username)
        await password.fill(self.config.password)
        submit = await first_visible(page, SEL_LOGIN_SUBMIT, timeout=2.0)
        if submit:
            await submit.click()
        else:
            await password.press("Enter")
        log("Kredensial dikirim, menunggu halaman tujuan ...")
        await self._settle()

        if await self._is_logged_in():
            log("Login berhasil")
            return

        error = await first_visible(page, SEL_LOGIN_ERROR, timeout=2.0)
        detail = clean_text(await error.text_content())[:160] if error else "tidak diketahui"
        raise SpadaError(f"Login gagal: {detail}")

    async def _is_logged_in(self) -> bool:
        """
        True bila halaman menunjukkan pengguna sudah masuk.

        Penanda utama adalah link logout / user menu, bukan URL, karena Moodle
        tetap berada di URL login sesaat setelah submit form.
        """
        assert self.page is not None
        markers = self.page.locator("a[href*='logout.php'], .usermenu, #usernavigation")
        if not await markers.count():
            return False
        try:
            body = await self.page.text_content("body")
        except PlaywrightError:
            return True
        return not (body and ERROR_PATTERN.search(body))

    # -- langkah 4 & 5: deteksi sesi dan submit ----------------------------

    async def submit_attendance(self, target: str) -> AttendanceResult:
        """
        Buka halaman attendance, deteksi sesi aktif, lalu isi "Hadir".

        Args:
            target: cmid atau URL penuh modul attendance.

        Returns:
            AttendanceResult dengan status success/already/not_open/failed.
        """
        assert self.page is not None
        page = self.page
        url = resolve_target(target)
        stamp = now_local().strftime("%H%M%S")
        course = f"cmid={target}"

        try:
            log(f"Membuka {url}")
            await page.goto(url, wait_until="domcontentloaded")
            await self._settle()

            course = await guess_course_name(page)
            if any(skip in course.lower() for skip in self.config.skip_courses):
                return AttendanceResult(course, "not_open", f"Diskip sesuai SKIP_COURSES: {course}")

            if await self._already_present(page):
                return AttendanceResult(course, "already", "Absensi hari ini sudah tercatat Hadir")

            link, row_text = await self._find_active_session(page)
            if link is None:
                await self.screenshot(f"no-session-{stamp}.png")
                return AttendanceResult(course, "not_open", session_reason(row_text))

            log(f"Sesi aktif: {row_text or 'form submit langsung'}")
            await link.click()
            await self._settle()

            if await self._already_present(page):
                return AttendanceResult(course, "already", "Absensi sudah tercatat Hadir")

            if not await self._fill_present(page):
                await self.screenshot(f"no-radio-{stamp}.png")
                return AttendanceResult(course, "failed", "Opsi 'Hadir/Present' tidak ditemukan")

            await self._save_changes(page)
            return await self._verify(page, course, stamp)

        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            reason = f"Selector timeout / error browser: {str(exc).splitlines()[0][:140]}"
        except SpadaError as exc:
            reason = str(exc)[:180]

        log(f"! {reason}")
        await self.screenshot(f"error-{stamp}.png")
        return AttendanceResult(course, "failed", reason)

    async def _session_rows(self, page: Page) -> Locator | None:
        """
        Kembalikan locator baris tabel sesi sesuai selector pertama yang cocok.

        Beberapa theme Moodle tidak memakai `table#attendancetable`, jadi semua
        kandidat dicoba dan yang punya isi dipakai. None bila tabel tidak ada.
        """
        for selector in SEL_SESSION_ROW:
            candidate = page.locator(selector)
            if await candidate.count() > 1:  # > 1 karena ada baris <thead>
                return candidate
        return None

    async def _find_active_session(self, page: Page) -> tuple[Locator | None, str]:
        """
        Cari baris sesi yang masih terbuka dan punya tombol submit.

        Returns:
            (tombol_submit, teks_baris_terakhir). Tombol None berarti tidak ada
            sesi aktif yang bisa diisi.
        """
        rows = await self._session_rows(page)
        if rows is None:
            return None, ""

        row_text = ""
        for index in range(1, await rows.count()):  # lewati baris <thead>
            row = rows.nth(index)
            text = clean_text(await row.text_content())
            if not text or text.lower().startswith(("date", "tanggal")):
                continue
            row_text = row_text or text
            if NOT_OPEN_PATTERN.search(text):
                continue
            link = await first_visible(row, SEL_SUBMIT_LINK, timeout=0.4)
            if link:
                return link, text

        # Fallback: beberapa halaman langsung menampilkan form attendance_submit.
        link = await first_visible(page, SEL_SUBMIT_LINK, timeout=1.0)
        if link and SUBMIT_PATTERN.search(clean_text(await link.text_content())):
            return link, row_text or "Form submit langsung"

        return None, row_text

    async def _already_present(self, page: Page) -> bool:
        """
        True bila absensi hari ini sudah tercatat.

        Dua strategi: sel status yang ditandai eksplisit, lalu fallback ke baris
        tabel sesi yang menyebut "Hadir/Present" tapi tanpa tombol submit lagi.
        """
        for selector in SEL_STATUS_CELL:
            cells = page.locator(selector)
            for index in range(await cells.count()):
                text = clean_text(await cells.nth(index).text_content())
                if text and PRESENT_PATTERN.search(text) and not NOT_OPEN_PATTERN.search(text):
                    return True

        rows = await self._session_rows(page)
        if rows is None:
            return False
        for index in range(1, await rows.count()):
            row = rows.nth(index)
            text = clean_text(await row.text_content())
            if not text or not PRESENT_PATTERN.search(text) or NOT_OPEN_PATTERN.search(text):
                continue
            if not await row.locator("a[href*='attendance_submit']").count():
                return True
        return False

    async def _fill_present(self, page: Page) -> bool:
        """
        Pilih radio "Hadir/Present".

        Plugin attendance Moodle memakai nilai 0 untuk Present, jadi nilai itu
        dicoba lebih dulu; bila tidak ada, ambil radio pertama yang terlihat.
        """
        radios = page.locator(SEL_PRESENCE_RADIO[0])
        total = await radios.count()
        if not total:
            return False

        for index in range(total):
            radio = radios.nth(index)
            if await radio.get_attribute("value") != "0":
                continue
            if await self._check(radio):
                log("Memilih status Hadir/Present")
                return True

        for index in range(total):
            radio = radios.nth(index)
            if await radio.is_visible() and await self._check(radio):
                log("Memilih opsi kehadiran pertama yang tersedia")
                return True
        return False

    async def _check(self, radio: Locator) -> bool:
        """Klik radio; return True bila berhasil dicentang."""
        try:
            await radio.check(timeout=3_000, force=True)
            return True
        except PlaywrightError:
            return False

    async def _save_changes(self, page: Page) -> None:
        """Klik tombol simpan; fallback tekan Enter pada form aktif."""
        button = await first_visible(page, SEL_SAVE_BUTTON, timeout=2.0)
        if button:
            await button.click()
        else:
            await page.keyboard.press("Enter")
        await self._settle()

    async def _verify(self, page: Page, course: str, stamp: str) -> AttendanceResult:
        """Pastikan absensi benar-benar tercatat setelah tombol simpan ditekan."""
        banner = await first_visible(page, SEL_SUCCESS_BANNER, timeout=2.0)
        if banner and PRESENT_PATTERN.search(clean_text(await banner.text_content())):
            return AttendanceResult(course, "success", "Dikonfirmasi banner Moodle")

        if await self._already_present(page):
            return AttendanceResult(course, "success", "Status berubah menjadi Hadir/Present")

        await self.screenshot(f"unverified-{stamp}.png")
        return AttendanceResult(
            course,
            "failed",
            "Status tidak berubah setelah submit (kemungkinan sesi sudah ditutup)",
        )

    # -- utilitas ----------------------------------------------------------

    async def _settle(self) -> None:
        """Tunggu jaringan idle, tapi jangan menggagalkan flow bila timeout."""
        assert self.page is not None
        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.config.timeout_ms)
        except PlaywrightTimeoutError:
            pass

    async def screenshot(self, filename: str) -> Path | None:
        """Simpan screenshot untuk debugging; kembalikan None bila gagal."""
        if self.page is None:
            return None
        path = self.config.artifact_dir / filename
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            log(f"Screenshot debugging: {path}")
            return path
        except PlaywrightError as exc:
            log(f"Gagal menyimpan screenshot: {exc}")
            return None


def session_reason(row_text: str) -> str:
    """Terjemahkan isi baris tabel menjadi alasan yang mudah dibaca."""
    if not row_text:
        return "No active session found (tabel sesi kosong atau tidak ditemukan)"
    if NOT_OPEN_PATTERN.search(row_text):
        return "Sesi sudah ditutup / belum dibuka"
    return f"No active session found. Sesi terakhir: {row_text[:120]}"


# ---------------------------------------------------------------------------
# Orkestrasi
# ---------------------------------------------------------------------------

STATUS_ICON = {"success": "✅", "already": "☑️", "not_open": "⏭️", "failed": "⚠️"}


def build_summary(results: list[AttendanceResult]) -> str:
    """Ringkasan seluruh mata kuliah dalam satu blok pesan."""
    counts = {status: sum(1 for r in results if r.status == status) for status in STATUS_ICON}
    lines = [
        "📋 *Ringkasan Presensi SPADA*",
        f"• *Waktu:* {now_local():%d %B %Y %H:%M} WIB",
        f"• *Berhasil:* {counts['success']}  |  *Sudah:* {counts['already']}  |  "
        f"*Dilewati:* {counts['not_open']}  |  *Gagal:* {counts['failed']}",
    ]
    lines += [f"{STATUS_ICON[r.status]} {r.course} — {r.reason or r.status}" for r in results]
    return "\n".join(lines)


def format_result(result: AttendanceResult) -> str:
    """Pilih template pesan sukses atau gagal sesuai status hasil."""
    if result.ok:
        return TelegramNotifier.format_success(result.course, result.timestamp, result.reason)
    return TelegramNotifier.format_failure(result.course, result.timestamp, result.reason)


async def run(config: Config) -> int:
    """
    Jalankan alur lengkap: login, submit per mata kuliah, notifikasi.

    Returns:
        0 bila tidak ada kegagalan, 1 bila ada error proses.
    """
    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)
    bot = SpadaBot(config)
    results: list[AttendanceResult] = []

    try:
        await bot.start()
        await bot.login()
        for target in config.attendance_targets:
            result = await bot.submit_attendance(target)
            results.append(result)
            await notifier.send(format_result(result))
    except (SpadaError, PlaywrightError) as exc:
        reason = clean_text(str(exc).splitlines()[0])[:180]
        log(f"! {reason}")
        label = f"{len(config.attendance_targets)} target attendance"
        results.append(AttendanceResult(label, "failed", reason))
        await bot.screenshot(f"fatal-{now_local():%H%M%S}.png")
        await notifier.send(TelegramNotifier.format_failure(label, now_local(), reason))
    finally:
        await bot.close()
        if results:
            await notifier.send(build_summary(results))

    return 1 if any(r.status == "failed" for r in results) else 0


def main() -> int:
    """Entry point CLI."""
    parser = argparse.ArgumentParser(
        description="Otomasi presensi SPADA UPN (Moodle) + notifikasi Telegram"
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        default=[],
        help="cmid atau URL modul attendance (bisa diulang)",
    )
    parser.add_argument(
        "--headed", action="store_true", help="Tampilkan jendela browser (override HEADLESS_MODE)"
    )
    args = parser.parse_args()

    try:
        config = load_config(cli_urls=args.urls, headed=True if args.headed else None)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    return asyncio.run(run(config))


if __name__ == "__main__":
    raise SystemExit(main())
